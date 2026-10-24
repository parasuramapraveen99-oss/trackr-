"use client";

/**
 * Risk & Return · Advanced (review §6, comments 10–12).
 *
 * Kept: volatility vs index, Sharpe/Sortino (risk-free rate named), the bad day (VaR, as
 * an exceedance threshold, never a maximum loss), the deepest fall vs the Nifty 50, the
 * daily-returns histogram. New: beta, Concentration impact (comment 11), an index-shock
 * table labelled as a rough estimate, liquidity. Removed: VaR 99 / CVaR / tracking error /
 * information ratio tiles and the Recorded/Hypothetical toggle.
 */

import { useState } from "react";
import type { AnalyticsResponse, RiskWindow } from "@trackr/core";
import { SegmentedControl } from "@trackr/ui";
import { BadDayHistogram, Underwater } from "../visual";
import { riskBoardData, type AdvisorPeriod } from "../AdvisorVisuals";
import { CoverageFooter, Grid, Info, Panel, Periods, Rs, Stat, faint, fmtFrac, fmtNum, fmtPct, muted, reasonText } from "./common";

const PERIODS: [AdvisorPeriod, string][] = [["1M", "1m"], ["3M", "3m"], ["6M", "6m"], ["1Y", "1y"], ["3Y", "3y"], ["5Y", "5y"], ["All", "available"]];

export function riskPeriods(data: AnalyticsResponse) {
  return PERIODS.map(([label, key]) => {
    const w = data.risk[key];
    const disabled = !w ? "Window longer than this portfolio's recorded history"
      : w.status === "unavailable" ? `Not available: ${(w.reasons ?? []).map(reasonText).join("; ")}` : null;
    return { key: label, label, disabled };
  });
}

function windowFor(data: AnalyticsResponse, period: AdvisorPeriod): RiskWindow | undefined {
  return data.risk[PERIODS.find(([p]) => p === period)![1]];
}

function ConcentrationImpact({ data }: { data: AnalyticsResponse }) {
  const impact = data.revamp?.risk.concentration_impact;
  const [n, setN] = useState<"3" | "5">("5");
  const [fall, setFall] = useState<"10" | "20" | "30">("20");
  if (!impact) return null;
  const opt = impact.options[n];
  const f = opt.falls[fall];
  return <Panel span={6} label="Concentration impact" info={<Info title="Concentration impact" calc={<>
    Loss = combined current value of the selected holdings × the assumed fall. Portfolio decline = loss ÷ total portfolio
    value (<Rs v={impact.total_value} />). Holdings are ranked by current value; inputs are unrounded and only the display is rounded.
  </>}>See how a fall in your largest holdings would affect your portfolio.</Info>}>
    <div style={muted}>See how a fall in your largest holdings would affect your portfolio.</div>
    <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
      <label style={faint}>Holdings included <SegmentedControl size="sm" value={n} onChange={setN} options={[{ value: "3", label: "Top 3" }, { value: "5", label: "Top 5" }]} /></label>
      <label style={faint}>Assumed fall in each selected stock <SegmentedControl size="sm" value={fall} onChange={setFall} options={[{ value: "10", label: "−10%" }, { value: "20", label: "−20%" }, { value: "30", label: "−30%" }]} /></label>
    </div>
    {opt.count < opt.requested && <div style={faint}>Only {opt.count} holdings exist, so all {opt.count} are used.</div>}
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
      {opt.holdings.map((h) => <span key={h.isin} style={{ padding: "2px 8px", borderRadius: 6, background: "var(--surface-sunken)", font: "500 12px var(--font-label)" }}>{h.symbol} {fmtPct(h.weight_pct, 1)}</span>)}
    </div>
    <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap", font: "600 20px var(--font-figure)" }}>
      <span>{fmtPct(opt.combined_weight_pct, 1)}</span><span style={faint}>combined weight ×</span>
      <span style={{ color: "var(--warning)" }}>−{fall}%</span><span style={faint}>hypothetical fall =</span>
      <span style={{ color: "var(--negative)" }}>−{fmtPct(f.portfolio_decline_pct, 1)}</span><span style={faint}>portfolio decline</span>
    </div>
    <div style={{ ...muted, color: "var(--negative)" }}>Estimated loss <Rs v={`-${f.loss}`} /></div>
    <div style={muted}>If your top {opt.count} holdings each fall {fall}%, your portfolio falls approximately {fmtPct(f.portfolio_decline_pct, 1)}.</div>
    <div style={faint}>All other prices stay unchanged. This scenario is not a forecast.</div>
  </Panel>;
}

function IndexShock({ data }: { data: AnalyticsResponse }) {
  const s = data.revamp?.risk.index_shock;
  if (!s) return null;
  const worst = s.worst_real_fall;
  return <Panel span={6} label="Index shock · rough estimate" info={<Info title="Index shock" calc={<>
    Each holding&apos;s estimated move = its value × the Nifty 50 move × its beta, measured from the past year&apos;s daily returns.
    Holdings without enough history are not estimated and the summed row says how much value it covers. Betas change;
    this is an estimate, not a forecast.
  </>}>What a fall in the Nifty 50 might do, if each holding moved as it has tended to with the index.</Info>}>
    <div className="rv-scroll"><table className="rv-table">
      <thead><tr><th>Nifty 50</th><th className="num">−10%</th><th className="num">−20%</th><th className="num">−30%</th></tr></thead>
      <tbody>
        <tr><td>Sum of holdings (beta × move)</td>{(["10", "20", "30"] as const).map((k) => <td key={k} className="num"><Rs v={s.summed[k]} /><div style={faint}>{fmtPct(s.summed_pct[k], 1)}</div></td>)}</tr>
        <tr><td>Portfolio beta {fmtNum(s.portfolio_beta, 2)}</td>{(["10", "20", "30"] as const).map((k) => <td key={k} className="num">{fmtPct(s.portfolio_beta_moves_pct?.[k], 1)}</td>)}</tr>
      </tbody>
    </table></div>
    <div style={faint}>Covers {fmtPct(s.covered_weight_pct, 0)} of value. The two rows differ because portfolio beta is measured on the weights you actually held through the year.</div>
    <div style={muted}>Your own worst real fall: {worst.max_drawdown != null ? <><b>{fmtFrac(worst.max_drawdown, 1)}</b> from {worst.peak} to {worst.trough}{worst.recovered ? `, recovered ${worst.recovered}` : ", not yet recovered"}</> : reasonText(worst.reason)}.</div>
    <details><summary style={{ cursor: "pointer", ...faint }}>Per holding</summary>
      <div className="rv-scroll"><table className="rv-table">
        <thead><tr><th>Holding</th><th className="num">Beta</th><th className="num">Nifty −10%</th><th className="num">−20%</th><th className="num">−30%</th></tr></thead>
        <tbody>{s.rows.map((r) => <tr key={r.isin}><td>{r.symbol}</td>
          {r.beta == null ? <td colSpan={4} style={faint}>Not estimated: {reasonText(r.reason)}</td> : <>
            <td className="num">{fmtNum(r.beta, 2)}</td>
            {(["10", "20", "30"] as const).map((k) => <td key={k} className="num"><Rs v={r.moves?.[k]} /></td>)}</>}
        </tr>)}</tbody>
      </table></div>
    </details>
  </Panel>;
}

function Liquidity({ data }: { data: AnalyticsResponse }) {
  const l = data.revamp?.risk.liquidity;
  if (!l) return null;
  const measured = l.rows.filter((r) => r.status === "measured");
  return <Panel label="How quickly you could sell" info={<Info title="Liquidity" calc={<>
    Days to exit = holding value ÷ ({Number(l.participation) * 100}% × the {l.sessions}-session median traded value across NSE and BSE).
    Selling more than about 10% of a stock&apos;s daily trading tends to move the price against you, so this assumes you sell at most that share each day.
    More than {l.thresholds_days.watch} days is watch; more than {l.thresholds_days.slow} days is slow.
  </>}>How many trading days it might take to sell each holding without moving its price much.</Info>}>
    {measured.length === 0 ? <div style={muted}>Not measured yet: traded value for {l.sessions} sessions is needed.</div> : <>
      <div style={muted}>{fmtPct(l.slow_weight_pct, 1)} of your money is in holdings needing more than {l.thresholds_days.slow} days to sell · measured for {fmtPct(l.measured_weight_pct, 0)} of value.</div>
      <div className="rv-scroll"><table className="rv-table">
        <thead><tr><th>Holding</th><th className="num">Value</th><th className="num">20-day median traded value</th><th className="num">Days to exit</th><th>Tier</th></tr></thead>
        <tbody>{l.rows.map((r) => <tr key={r.isin}><td>{r.symbol}</td><td className="num"><Rs v={r.market_value} /></td>
          {r.status === "measured" ? <>
            <td className="num"><Rs v={r.median_traded_value} /></td>
            <td className="num">{Number(r.days_to_exit) > 0 && Number(r.days_to_exit) < 0.1 ? "<0.1" : fmtNum(Number(r.days_to_exit), 1)}</td>
            <td style={{ color: r.tier === "slow" ? "var(--negative)" : r.tier === "watch" ? "var(--warning)" : "var(--positive)" }}>{r.tier === "slow" ? "Slow" : r.tier === "watch" ? "Watch" : "Easy"}</td>
          </> : <td colSpan={3} style={faint}>{r.status === "not_exchange_traded" ? "Not exchange-traded, not measured" : `Not measured: ${r.sessions ?? 0} of ${r.sessions_needed} traded sessions`}</td>}
        </tr>)}</tbody>
      </table></div>
    </>}
    <div style={faint}>Price-limit (circuit) hits are not measured yet: the daily price-band file is not ingested.</div>
  </Panel>;
}

export function RiskAdvanced({ data }: { data: AnalyticsResponse }) {
  const options = riskPeriods(data);
  const [period, setPeriod] = useState<AdvisorPeriod>((options.find((o) => o.key === "1Y" && !o.disabled)?.key ?? options.find((o) => !o.disabled)?.key ?? "1Y") as AdvisorPeriod);
  const w = windowFor(data, period);
  const rf = data.sources?.risk_free_rate;
  const benchDd = period === "1Y" ? data.revamp?.risk.benchmark_drawdown : undefined;
  const board = riskBoardData(data, period, "recorded");
  return <Grid>
    <div style={{ gridColumn: "1 / -1", display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
      <Periods options={options} value={period} onChange={(k) => setPeriod(k as AdvisorPeriod)} />
      <span style={faint}>{w?.period?.start ? `${w.period.start} – ${w.period.end} · ${w.observations} daily returns` : ""}</span>
    </div>
    <Stat label="Volatility" value={fmtFrac(w?.volatility, 1)} sub={<>Nifty 50 {fmtFrac(w?.bench_volatility, 1)}{w?.volatility && w?.bench_volatility ? ` · ${(w.volatility / w.bench_volatility).toFixed(1)}× the index` : ""}</>}
      info={<Info title="Volatility" calc="Standard deviation of daily time-weighted returns × √252, against the Nifty 50 TRI over the same sessions.">How much the portfolio&apos;s value swings in a typical year. It describes the past, not a limit on the future.</Info>} />
    <Stat label="Beta" value={fmtNum(w?.beta, 2)} sub="vs Nifty 50 TRI · daily returns"
      info={<Info title="Beta" calc="Slope of the portfolio's daily returns regressed on the Nifty 50 TRI's, same sessions.">When the Nifty 50 has moved 1%, your portfolio has tended to move this many percent.</Info>} />
    <Stat label="Sharpe" span={2} value={fmtNum(w?.sharpe, 2)} sub={`Nifty 50 ${fmtNum(w?.bench_sharpe, 2)}`}
      info={<Info title="Sharpe ratio" calc={<>Annualised excess return over the risk-free rate ÷ annualised volatility. Risk-free: {rf ?? "not recorded"} ({fmtFrac(w?.risk_free_rate, 2)} over this window).</>}>Return earned per unit of swing, after what a risk-free deposit would have paid.</Info>} />
    <Stat label="Sortino" span={2} value={fmtNum(w?.sortino, 2)} sub={w?.sortino_target?.includes("risk_free") ? "downside below the risk-free rate" : `downside below ${w?.sortino_target ?? "0"}`}
      info={<Info title="Sortino ratio" calc="Like Sharpe, but divides by downside deviation only (returns below the target).">Return per unit of downside swing.</Info>} />
    <Stat label="Bad day (1 in 20)" span={2} value={w?.var_1d_95_value ? <Rs v={w.var_1d_95_value} /> : <span style={{ fontSize: 15, color: "var(--text-secondary)" }}>Not measured</span>}
      sub={w?.var_1d_95 != null ? `${fmtFrac(w.var_1d_95, 2)} of today's worth` : `${w?.observations ?? 0} of 252 daily returns`}
      info={<Info title="Value at risk, 1 day, 95%" calc="Historical method: the 5th-percentile daily return of the window × today's worth. Shown once 252 daily returns exist (app policy, not a guarantee).">A loss exceeded on about 1 trading day in 20 in the past. It is a threshold, not the most you can lose.</Info>} />
    <Panel span={7} label={`Deepest fall · ${period}`} info={<Info title="Deepest fall" calc="Largest fall from a running peak of the cash-flow-adjusted value series; the Nifty 50 TRI over the same sessions.">How far the portfolio fell from a high point, and whether it has climbed back.</Info>}
      right={<span style={faint}>{w?.max_drawdown != null ? <>You {fmtFrac(w.max_drawdown, 1)}{w.drawdown_dates?.recovered ? ` · recovered ${w.drawdown_dates.recovered}` : " · not yet recovered"}</> : ""}
        {benchDd?.max_drawdown != null && <> · Nifty 50 {fmtFrac(benchDd.max_drawdown, 1)}</>}</span>}>
      <Underwater data={board} />
    </Panel>
    <Panel span={5} label="Daily returns" info={<Info title="Daily returns">Each bar counts days by how much the portfolio moved. The left tail is where the bad-day figure comes from.</Info>}>
      <BadDayHistogram data={board} />
    </Panel>
    <ConcentrationImpact data={data} />
    <IndexShock data={data} />
    <Liquidity data={data} />
    <CoverageFooter data={data} />
  </Grid>;
}
