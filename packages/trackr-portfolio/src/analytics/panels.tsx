"use client";

/** Advisor analytics: recorded investor outcomes, exposures and inspectable inputs. */
import { useState, type ReactNode } from "react";
import {
  ALLOC_RAMP, Badge, Card, DataTable, DonutChart, HeatMap,
  ScatterPlot, VintageLinePlot, type BubblePoint, type DataColumn,
} from "@trackr/ui";
import { Figure, parseDecimal } from "@trackr/core";
import type { AnalyticsPosition, AnalyticsResponse, CashFlowWindow, RiskCoverage } from "@trackr/core";
import { EMPTY, money, pct, pp, ratio } from "./format";

const stack: React.CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--gap-grid)" };
const grid: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: "var(--gap-grid)" };
const note: React.CSSProperties = { fontSize: 12, color: "var(--text-secondary)", marginTop: 10, lineHeight: 1.6 };
const button = (active: boolean): React.CSSProperties => ({
  background: active ? "var(--ink-900)" : "var(--surface)",
  color: active ? "var(--text-inverse)" : "var(--text-secondary)",
  border: "1px solid var(--border)", borderRadius: 7, padding: "5px 11px",
  font: "inherit", fontSize: 12, cursor: "pointer",
});
const columns = (rows: DataColumn<Record<string, unknown>>[]) => rows;
const ordered = (data: AnalyticsResponse) => [...data.positions].sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0));

/** A multiple such as debt/equity: 0.33 -> "0.33×". */
const times = (value: number | null | undefined) => (value == null || !Number.isFinite(value) ? EMPTY : `${ratio(value)}×`);

/** Ledger rates remain decimal strings all the way to their final display rounding. */
function rate(value: string | null | undefined, fraction = true) {
  const d = parseDecimal(value);
  return d == null ? EMPTY : `${fraction ? d.times(100).toFixed(2) : d.toFixed(2)}${fraction ? "%" : " pp"}`;
}

function Reading({ label, value, detail }: { label: string; value: ReactNode; detail?: ReactNode }) {
  return <div style={{ borderBottom: "1px solid var(--border)", padding: "11px 0" }}>
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
      <span style={{ fontSize: 13 }}>{label}</span>
      <span style={{ fontFamily: "Newsreader, serif", fontSize: 23, fontVariantNumeric: "tabular-nums" }}>{value}</span>
    </div>
    {detail && <div style={note}>{detail}</div>}
  </div>;
}

function Coverage({ value }: { value: number | undefined }) {
  return <Badge tone={value != null && value < 95 ? "warning" : "neutral"}>Coverage {pp(value, 1)} of value</Badge>;
}

function WindowCoverage({ coverage }: { coverage: RiskCoverage | undefined }) {
  if (!coverage) return null;
  return <div style={{ ...note, border: "1px solid var(--border)", padding: 12, margin: "0 0 16px" }}>
    <Coverage value={coverage.weight_pct} /> {coverage.start} to {coverage.end} · {coverage.observations} daily returns · {coverage.included_isins.length} holdings
    <div>Today's weights, rebalanced daily over the covered holdings. Historical price returns; trading costs and cash dividends are excluded.</div>
    {coverage.excluded.length > 0 && <div><b>Excluded:</b> {coverage.excluded.map((p) => `${p.symbol}: ${p.reason}`).join("; ")}</div>}
    <div>Rupee estimates apply to the covered value <Figure>{money(coverage.market_value)}</Figure>; uncovered holdings add unmeasured risk.</div>
  </div>;
}

function AssetCell({ position: p }: { position: AnalyticsPosition }) {
  return <div><b>{p.symbol ?? p.isin}</b><div style={{ fontSize: 11, color: "var(--text-tertiary)" }}>{p.name}</div></div>;
}

function ReturnCell({ position: p, window }: { position: AnalyticsPosition; window: string }) {
  const observation = p.return_windows?.[window];
  return <span title={observation?.start ? `${observation.start} to ${observation.end}; action-adjusted price return, excludes dividends` : "Missing endpoint price for this calendar period"}>
    {pp(p.returns?.[window], 1, true)}
  </span>;
}

function CashFlowCard({ data, window, label }: { data: AnalyticsResponse; window: CashFlowWindow | undefined; label: string }) {
  if (!window) return <Card title={`Recorded return · ${label}`}><p style={note}>Insufficient recorded history for this period.</p></Card>;
  return <Card title={`Recorded return · ${label}`} subtitle={`${window.start} to ${window.end} · annualised, money-weighted`}>
    <Coverage value={window.coverage_pct} />
    <Reading label="Security cash-flow XIRR" value={rate(window.xirr)} detail="Purchases and charges out; sales, buybacks and recorded dividends in; closing holdings marked to the selected date." />
    <Reading label={`Cash-flow-matched ${data.benchmark} XIRR`} value={rate(window.benchmark_xirr)} detail="A hypothetical index investment with the same dated cash flows and, for a period, the same opening value. Includes index dividend reinvestment." />
    <Reading label="XIRR difference" value={rate(window.relative_pp, false)} />
    {window.benchmark_terminal_value != null && <Reading label="Equivalent index closing value" value={<Figure>{money(window.benchmark_terminal_value)}</Figure>} detail={<>Actual covered closing value <Figure>{money(window.market_value)}</Figure> · difference <Figure>{money(window.terminal_gap, true)}</Figure></>} />}
    {window.excluded.length > 0 && <p style={note}>Excluded from both sides: {window.excluded.map((p) => `${p.symbol}: ${p.reason}`).join("; ")}</p>}
    {window.benchmark_missing_dates.length > 0 && <p style={note}>Index comparison unavailable; missing levels on {window.benchmark_missing_dates.join(", ")}.</p>}
  </Card>;
}

function CashFlowDisclosure({ data }: { data: AnalyticsResponse }) {
  const p = data.performance;
  return <div style={note}>
    {p?.basis ?? "Recorded security cash flows; idle cash is excluded."}
    {p?.recorded_dividend_count === 0 && <div><b>No cash dividends are recorded.</b> Dividends absent from the ledger are absent from the investor return; this is not a verified account total return.</div>}
    {(p?.auto_resolved_cash_flow_count ?? 0) > 0 && <div>{p.auto_resolved_cash_flow_count} purchase cash flows were automatically resolved; inspect their source evidence before treating the return as final.</div>}
    <div>XIRR reflects cash-flow timing. Adviser skill requires cash-flow-neutral TWRR from complete account valuations and external cash flows; the price simulation below does not provide that.</div>
  </div>;
}

const readableReason = (reason: string) => reason.toLowerCase().replaceAll("_", " ");
const score = (value: number | null | undefined) => (value == null ? EMPTY : value.toFixed(1));

/** portfolio-health-v1: every part, its weight and points, and why a part is missing. */
function AdvisorHealthCard({ data }: { data: AnalyticsResponse }) {
  const h = data.health;
  const ev = h.evidence ?? {};
  const symbol = (isin: string) => data.positions.find((p) => p.isin === isin)?.symbol ?? isin;
  const unprofiled = Object.entries(h.profile_status ?? {}).filter(([, s]) => s !== "bank" && s !== "non_financial");
  const cov = ev.covariance?.value;
  const recovery = ev.recovery?.value;
  const weights = ev.weight_sensitivity?.value;
  const cutoffs = ev.grade_cutoff_sensitivity?.value;
  return <Card title="Portfolio health · house rubric v1" subtitle="Concentration, co-movement, past risk, business quality and relative valuation. Provisional and uncalibrated; not advice.">
    <Reading label="Grade" value={h.grade ?? EMPTY}
      detail={h.grade != null
        ? <>Score {h.score_exact ? Number(h.score_exact).toFixed(2) : h.score}/100, rounded half-up to {h.score}. Bands {h.bands.map(([g, t]) => `${g} ≥ ${t}`).join(" · ")}.</>
        : h.score_bounds
          ? <>Withheld. With the parts measured so far the score can only fall between {score(h.score_bounds.lower)} and {score(h.score_bounds.upper)}; this is a missing-information range, not a forecast.</>
          : <>Withheld · {(h.reasons ?? []).map(readableReason).join(" · ")}</>} />
    <DataTable dense columns={columns([{ key: "part", label: "Part" }, { key: "weight", label: "Weight", align: "right" }, { key: "score", label: "Score", align: "right" }, { key: "points", label: "Points", align: "right" }, { key: "why", label: "Status" }])}
      rows={h.components.map((c) => ({ id: c.key, part: c.label, weight: pp(c.weight * 100, 0), score: score(c.score), points: score(c.points), why: c.score == null ? (c.reasons ?? []).map(readableReason).join(" · ") : c.status ?? "" }))} />
    {unprofiled.length > 0 && <div style={note}>Business quality has no supported test for: {unprofiled.map(([isin, s]) => {
      const [kind = s, industry] = s.split(":");
      return `${symbol(isin)} (${readableReason(kind)}${industry ? ` · ${industry}` : ""})`;
    }).join(", ")}. Banks and non-financial companies are scored; NBFCs and insurers need their own tests and are not guessed.</div>}
    {cov && <>
      <Reading label="Diversification ratio" value={ratio(cov.diversification_volatility_ratio)} detail={<>Average holding volatility ÷ portfolio volatility ({pct(cov.annual_volatility)} a year, {cov.observations} sessions). Above 1 means holdings partly offset each other.</>} />
      <DataTable dense columns={columns([{ key: "asset", label: "Holding" }, { key: "share", label: "Share of risk", align: "right" }])}
        rows={Object.entries(cov.risk_shares).sort((a, b) => (b[1].risk_share ?? 0) - (a[1].risk_share ?? 0)).map(([isin, r]) => ({ id: isin, asset: symbol(isin), share: pct(r.risk_share, 1) }))} />
      <div style={note}>Estimator check (shrink toward equal variances, not Ledoit–Wolf): {Object.entries(cov.shrinkage_sensitivity).map(([a, v]) => `α ${a}: ${pct(v?.annual_volatility ?? null)}`).join(" · ")}. Uses today&apos;s weights on past returns, not your account history.</div>
    </>}
    {recovery && <Reading label="Longest time below a previous high" value={`${recovery.longest_underwater_sessions} sessions`}
      detail={<>Currently {recovery.current_underwater_sessions} sessions below the high ({pct(recovery.terminal_drawdown)}). Only counted inside the one-year window; an unfinished recovery has no invented end date.</>} />}
    {weights && <div style={note}>If each part&apos;s weight moved ±{pct(weights.relative_weight_change, 0)}, the score would range {score(weights.minimum)}–{score(weights.maximum)} (grades {weights.grades.join(", ")}).{cutoffs && ` Moving every grade cut-off ±5 points gives ${Object.values(cutoffs).join(" / ")}.`}</div>}
    <div style={note}>Validation: calibration {ev.validation?.grade_calibration ?? "pending"} · Indian data check {ev.validation?.indian_equity_mf_validation ?? "pending"} · independent review {ev.validation?.independent_human_review ?? "pending"}.</div>
  </Card>;
}

export function OverviewPanel({ data }: { data: AnalyticsResponse }) {
  const c = data.concentration;
  const available = data.health.components.filter((p) => p.score != null).length;
  return <div style={stack}>
    <div style={grid}>
      <CashFlowCard data={data} window={data.performance?.windows["1y"] ?? data.performance?.windows.all} label={data.performance?.windows["1y"] ? "1Y" : "all history"} />
      <Card title="Where the money is concentrated" subtitle="Market-value exposure, independent of return forecasts.">
        <Reading label="Largest holding" value={pp(c?.top1_pct, 1)} />
        <Reading label="Top three holdings" value={pp(c?.top3_pct, 1)} />
        <Reading label="Top five holdings" value={pp(c?.top5_pct, 1)} />
        <Reading label="Effective holdings · 1 / HHI" value={ratio(data.derived.effective_holdings, 1)} />
        <div style={note}>{c?.leaders.map((p) => `${p.symbol} ${pp(p.weight_pct, 1)}`).join(" · ")}</div>
      </Card>
    </div>
    <CashFlowDisclosure data={data} />
    {data.advisor && <AdvisorHealthCard data={data} />}
    <Card title="Data to review" subtitle="Visible limitations, with affected holdings named.">
      <Reading label="Sector classification" value={<Coverage value={data.coverage.sector_pct} />} detail={ordered(data).filter((p) => !p.sector).map((p) => `${p.symbol ?? p.isin} ${pp(p.weight, 1)}`).join(" · ")} />
      <Reading label="P/B inputs" value={<Coverage value={data.coverage.pb_pct} />} detail={ordered(data).filter((p) => p.pb == null).map((p) => p.symbol ?? p.isin).join(", ")} />
      <Reading label="Debt/equity inputs" value={<Coverage value={data.coverage.debt_to_equity_pct} />} detail={ordered(data).filter((p) => p.debt_to_equity == null).map((p) => p.symbol ?? p.isin).join(", ")} />
      <div style={note}>
        {data.health.grade == null ? `Health grade withheld · partial, ${available} of ${data.health.components.length} inputs available.` : "Health rubric is a house model, not an independently validated investment assessment."}
        {Object.values(data.factors).every((f) => f.score == null) && " No eligible market ranking universe; holdings-only factor ranks do not enter the grade."}
      </div>
      {data.price_events.length > 0 && <details style={note}><summary>{data.price_events.length} one-day price moves above 15% inside the risk windows · largest positions first</summary>
        <DataTable dense columns={columns([{ key: "asset", label: "Holding" }, { key: "weight", label: "Value weight", align: "right" }, { key: "date", label: "Observed interval" }, { key: "move", label: "Move", align: "right" }, { key: "reason", label: "Likely explanation" }])}
          rows={data.price_events.map((p) => ({ id: `${p.isin}:${p.date}`, asset: p.symbol, weight: pp(p.weight_pct, 1), date: `${p.previous_date} → ${p.date}`, move: pp(p.change_pct, 1, true), reason: p.reason }))} />
      </details>}
    </Card>
    <details style={note}><summary>Profit and trend distribution · descriptive only</summary>
      <Card title="Profit and 50-observation trend" subtitle={data.advisor
        ? "In profit or loss × latest price above or below its 50-session average. An exact tie is neutral; missing history is unknown and still counted."
        : "Within 2% of the moving average counts as flat. Missing history has its own bucket."}>
        {Object.keys(data.capital_state).length === 0
          ? <p style={note}>{data.advisor ? `Not available · ${(data.health.reasons ?? []).map(readableReason).join(" · ") || "no complete valuation"}.` : EMPTY}</p>
          : Object.entries(data.capital_state).map(([key, value]) => <Reading key={key} label={key} value={<Figure>{money(value)}</Figure>}
            detail={<>{pp(data.derived.capital_state_pct[key], 1)}{key === "UNKNOWN" && ` · ${data.positions.filter((p) => p.capital_state === "UNKNOWN").map((p) => `${p.symbol ?? p.isin}: ${readableReason(p.capital_state_reason ?? "unknown")}`).join(", ")}`}</>} />)}
      </Card>
    </details>
  </div>;
}

function RawHoldings({ positions }: { data: AnalyticsResponse; positions: AnalyticsPosition[] }) {
  return <DataTable dense columns={columns([
    { key: "asset", label: "Holding" }, { key: "weight", label: "Value weight", align: "right" },
    { key: "pe", label: "P/E", align: "right" }, { key: "pb", label: "P/B", align: "right" },
    { key: "roe", label: "Company ROE", align: "right" }, { key: "de", label: "D/E", align: "right" },
    { key: "return", label: "1Y price return", align: "right" }, { key: "momentum", label: "12–1 price return", align: "right" },
    { key: "inputs", label: "Input coverage / provenance" },
  ])} rows={positions.map((p) => ({ id: p.isin, asset: <AssetCell position={p} />, weight: pp(p.weight, 1),
    pe: ratio(p.pe), pb: ratio(p.pb), roe: pct(p.roe, 1), de: times(p.debt_to_equity),
    return: <ReturnCell position={p} window="1y" />, momentum: pct(p.momentum_12_1, 1, true),
    inputs: <div style={{ maxWidth: 260, fontSize: 11 }}>{p.input_notes?.join("; ")}
      <div>{p.fundamentals_source ?? "No fundamentals feed"} · observed {p.fundamentals_as_of ?? EMPTY} · fiscal {p.fiscal_period ?? EMPTY}</div>
      {p.roe_basis && <div>ROE: {p.roe_basis.replaceAll("_", " ")}</div>}
    </div>,
  }))} />;
}

export function FactorPanel({ data }: { data: AnalyticsResponse }) {
  const positions = ordered(data);
  const small = positions.filter((p) => (p.weight ?? 0) < 1);
  const big = positions.filter((p) => (p.weight ?? 0) >= 1);
  return <div style={stack}>
    <FundamentalReadings data={data} />
    <Card title="Largest holdings · absolute characteristics" subtitle="Sorted by market value. Raw inputs describe company exposures; they do not imply cheapness or investment quality.">
      <RawHoldings data={data} positions={big} />
      {small.length > 0 && <details style={note}>
        <summary>Other holdings below 1% each · {small.length} names · {pp(small.reduce((s, p) => s + (p.weight ?? 0), 0), 2)} of portfolio value</summary>
        <RawHoldings data={data} positions={small} />
      </details>}
      <div style={note}>12–1 is a plain price return from twelve months ago to one month ago. It excludes the latest month and is not risk-adjusted. Cash dividends are excluded. Hover 1Y returns for the actual observed endpoints.</div>
      <div style={note}>Percentile meters are withheld without a real reference universe. A within-portfolio rank is not a market valuation or quality assessment.</div>
    </Card>
  </div>;
}

export function RiskPanel({ data }: { data: AnalyticsResponse }) {
  const available = Object.keys(data.risk);
  const [selected, setSelected] = useState(available[0] ?? "1y");
  const key = data.risk[selected] ? selected : available[0];
  const r = key ? data.risk[key] : undefined;
  if (!r) return <Card title="Current-holdings risk"><p style={note}>Insufficient observed price history. No portfolio risk estimate is substituted.</p></Card>;
  const betaLabel = r.beta == null ? "Insufficient benchmark data" : r.beta > 1.2 ? "Higher sensitivity than the index" : r.beta < .8 ? "Lower sensitivity than the index" : "Close to market sensitivity";
  return <div style={stack}>
    <Card title="Current-holdings risk · historical simulation" subtitle="Hypothetical daily rebalancing of today's positions; these are not recorded account returns."
      action={<div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{available.map((w) => <button key={w} style={button(key === w)} onClick={() => setSelected(w)}>{w === "available" ? "Available window" : w.toUpperCase()}</button>)}</div>}>
      <WindowCoverage coverage={key ? data.risk_coverage?.[key] : undefined} />
      <div style={grid}>
        <Reading label="Annualised volatility" value={pct(r.volatility)} detail={<>Index: {pct(r.bench_volatility)} · one-day standard-deviation move <Figure>{money(r.volatility_1d_value)}</Figure>. Not a loss bound.</>} />
        <Reading label="Maximum drawdown" value={pct(r.max_drawdown)} detail={<>Peak {r.drawdown_dates?.peak ?? EMPTY} → trough {r.drawdown_dates?.trough ?? EMPTY} · recovered {r.drawdown_dates?.recovered ?? "not within the window"}.</>} />
        <Reading label="1-day historical VaR · 95%" value={<Figure>{money(r.var_1d_95_value)}</Figure>} detail={<>{pct(r.var_1d_95)} of covered value; empirical 5th-percentile loss. Losses beyond this estimate occur in the historical tail.</>} />
        <Reading label="Expected shortfall · worst 5%" value={<Figure>{money(r.cvar_1d_95_value)}</Figure>} detail={<>{pct(r.cvar_1d_95)} of covered value; mean loss over the worst 5% of observed probability mass.{r.cvar_1d_95 != null && r.cvar_1d_95 < 0 && " Negative: even the worst 5% of sessions in this window were gains."}</>} />
        <Reading label="Beta" value={ratio(r.beta)} detail={<>Index: 1.00 · {betaLabel}. Covariance with index returns divided by index variance; no better/worse judgement.</>} />
        <Reading label="Simulated cumulative price return" value={pct(r.cum_return)} detail={<>Index total return: {pct(r.bench_cum_return)}. Dividend and transaction-cost treatment differs; use recorded cash-flow performance to assess investor outcomes.</>} />
      </div>
      <div style={note}>{r.risk_free_rate == null ? "Sharpe and Sortino withheld: no sourced risk-free return series is available." : `Risk-free annual rate ${pct(r.risk_free_rate)} · Sharpe ${ratio(r.sharpe)} · Sortino ${ratio(r.sortino)}.`}</div>
    </Card>
    <Card title="Which positions contribute to simulated risk?" subtitle="Euler covariance contribution to volatility, with position weight beside it. Negative contributions are retained.">
      <DataTable dense columns={columns([{ key: "asset", label: "Holding" }, { key: "weight", label: "Portfolio value weight", align: "right" }, { key: "risk", label: "Share of covered risk", align: "right" }])}
        rows={(key ? data.risk_contributions[key] ?? [] : []).map((p) => ({ id: p.isin, asset: p.symbol, weight: pp(p.weight_pct, 1), risk: pp(p.risk_share_pct, 1) }))} />
      <div style={note}>Contribution = weight × covariance with portfolio returns / portfolio variance. Shares sum to 100% over covered holdings; they are historical estimates, not forecasts. Large moves require reviewing the underlying price and corporate-action data.</div>
    </Card>
  </div>;
}

export function DiversificationPanel({ data }: { data: AnalyticsResponse }) {
  const groups = [["Market capitalisation", "market_cap", "market_cap_bucket"], ["Sectors", "sector", "sector"]] as const;
  const positions = ordered(data);
  return <div style={stack}>
    <Card title="Concentration by money" subtitle="Holding counts alone do not show exposure.">
      <div style={grid}><Reading label="Top three" value={pp(data.concentration?.top3_pct, 1)} /><Reading label="Top five" value={pp(data.concentration?.top5_pct, 1)} /><Reading label="Effective holdings · 1 / HHI" value={ratio(data.derived.effective_holdings, 1)} /></div>
      <div style={note}>{data.concentration?.leaders.map((p) => `${p.symbol} ${pp(p.weight_pct, 1)}`).join(" · ")}</div>
    </Card>
    <div style={grid}>{groups.map(([title, key, field]) => {
      const missing = positions.filter((p) => !p[field]);
      const gap = missing.reduce((s, p) => s + (p.weight ?? 0), 0);
      return <Card key={key} title={title} subtitle={key === "market_cap" ? "AMFI classification by rank: 1–100 large, 101–250 mid, remaining classified names small." : "Stored industry classifications; missing names remain unclassified."}>
        <Coverage value={100 - gap} />
        <DonutChart slices={data.allocation[key].map((p, i) => ({ label: p.label, value: p.pct, display: pp(p.pct, 1), sub: <Figure>{money(p.value)}</Figure>, color: ALLOC_RAMP[i % ALLOC_RAMP.length] }))} size={200} thickness={26} legendRuled />
        {missing.length > 0 && <div style={note}><b>Unclassified:</b> {missing.map((p) => `${p.symbol ?? p.isin} ${pp(p.weight, 1)}`).join(" · ")}</div>}
      </Card>;
    })}</div>
    <Card title="Observed holding price returns" subtitle="Calendar periods; each endpoint must have an observed close within five days. Cash dividends are excluded.">
      <DataTable dense columns={columns([{ key: "asset", label: "Holding" }, { key: "weight", label: "Value weight", align: "right" }, ...["1w", "1m", "3m", "1y", "3y", "5y"].map((k) => ({ key: k, label: k.toUpperCase(), align: "right" as const }))])}
        rows={positions.map((p) => ({ id: p.isin, asset: <AssetCell position={p} />, weight: pp(p.weight, 1), ...Object.fromEntries(["1w", "1m", "3m", "1y", "3y", "5y"].map((k) => [k, <ReturnCell key={k} position={p} window={k} />])) }))} />
      <div style={note}>Hover a return to inspect its actual quote dates. A blank identifies a missing endpoint; intermediate gaps prevent daily-risk estimates but do not prevent an observed endpoint price ratio.</div>
    </Card>
    <details style={note}><summary>Pairwise correlation diagnostic · unweighted, not a portfolio diversification score</summary>
      <Card title="Daily-return correlations"><WindowCoverage coverage={data.risk_coverage?.[data.risk["1y"] ? "1y" : "available"]} />
        {data.correlation.symbols.length > 1 ? <HeatMap labels={data.correlation.symbols} matrix={data.correlation.matrix} /> : <p>Insufficient shared history.</p>}
        <div style={note}>Every pair counts equally in this diagnostic. Use position weights and the risk-contribution table to assess the portfolio.</div>
      </Card>
    </details>
  </div>;
}

function FundamentalReadings({ data }: { data: AnalyticsResponse }) {
  const f = data.fundamentals;
  const c = data.characteristics;
  const rows: [string, number | null | undefined, number | undefined, boolean, string][] = [
    ["P/E · harmonic mean", f.pe.value, 100 - f.pe.excluded_weight_pct, false, "Covered weight / sum(weight ÷ positive P/E); excludes unavailable and nonpositive P/Es."],
    ["P/B · harmonic mean", f.pb.value, 100 - f.pb.excluded_weight_pct, false, "Covered weight / sum(weight ÷ positive P/B)."],
    ["Dividend yield · weighted", f.dividend_yield.value, 100 - f.dividend_yield.excluded_weight_pct, true, "Vendor yield exposure; not the cash dividend income recorded in your account."],
    ["P/E · median by holding", c.median_pe?.value, c.median_pe?.coverage_pct, false, "Each covered company counts once; less sensitive to a single low P/E."],
    ["P/E · median by money", c.weighted_median_pe?.value, c.weighted_median_pe?.coverage_pct, false, "P/E at the halfway point of covered market value."],
    ["Company ROE · weighted", c.roe?.value, c.roe?.coverage_pct, true, "Average company ROE exposure; not consolidated portfolio return on equity. Feed and statement periods may differ."],
    ["Company D/E · weighted", c.debt_to_equity?.value, c.debt_to_equity?.coverage_pct, false, `Company debt/equity exposure (0.50× = debt is half of equity). ${c.debt_to_equity?.note ?? ""}`],
    ["12–1 price return · weighted", c.momentum_12_1?.value, c.momentum_12_1?.coverage_pct, true, "Today's weights applied to each holding's raw twelve-to-one-month price return; excludes dividends."],
  ];
  return <Card title="Absolute portfolio characteristics" subtitle="Covered subsets, using market-value weights. These are inputs for analysis, not buy/sell judgements.">
    <div style={grid}>{rows.map(([label, value, coverage, fraction, detail]) => <Reading key={label} label={label} value={fraction ? pct(value, 2) : label.includes("D/E") ? times(value) : ratio(value)} detail={<><Coverage value={coverage} /><div>{detail}</div></>} />)}</div>
  </Card>;
}

export function FundamentalsPanel({ data }: { data: AnalyticsResponse }) {
  const f = data.fundamentals;
  return <div style={stack}>
    <FundamentalReadings data={data} />
    <Card title={`${data.benchmark} · published index characteristics`} subtitle={`Observed ${f.benchmark_as_of ?? "not available"}; a different company mix from your portfolio.`}>
      <div style={grid}><Reading label="Index P/E" value={ratio(f.benchmark.pe)} /><Reading label="Index P/B" value={ratio(f.benchmark.pb)} /><Reading label="Index dividend yield" value={pct(f.benchmark.dividend_yield)} /></div>
      <div style={note}>{f.benchmark_source?.startsWith("https://") ? <a href={f.benchmark_source} target="_blank" rel="noreferrer">NSE Indices source</a> : f.benchmark_source}</div>
      <div style={note}>Same aggregation on both sides: the index P/E is free-float market value ÷ earnings, and the portfolio P/E is holding value ÷ attributable trailing earnings (the value-weighted harmonic mean). They differ in inputs: the portfolio uses cached vendor trailing earnings and prices at each holding's observation date, while the index uses the publisher's free-float data. The two books also hold different companies, so this comparison does not establish fair value.</div>
    </Card>
    <Card title="Inspect the aggregate inputs" subtitle="Largest positions first. Low P/E is a review flag, not proof of a data error or an investment opportunity.">
      <DataTable dense columns={columns([{ key: "asset", label: "Holding" }, { key: "weight", label: "Value weight", align: "right" }, { key: "pe", label: "P/E", align: "right" }, { key: "share", label: "Share of weighted earnings yield", align: "right" }, { key: "notes", label: "Provenance / gaps" }])}
        rows={ordered(data).map((p) => ({ id: p.isin, asset: <AssetCell position={p} />, weight: pp(p.weight, 1), pe: ratio(p.pe), share: pp(p.earnings_yield_contribution_pct, 1), notes: <div style={{ maxWidth: 360, fontSize: 11 }}>{p.input_notes?.join("; ")}<div>{p.fundamentals_source ?? "No source"} · observed {p.fundamentals_as_of ?? EMPTY} · fiscal period {p.fiscal_period ?? EMPTY}</div></div> }))} />
    </Card>
  </div>;
}

export function PerformancePanel({ data }: { data: AnalyticsResponse }) {
  const available = Object.keys(data.performance?.windows ?? {});
  const [selected, setSelected] = useState(available.includes("1y") ? "1y" : "all");
  const key = data.performance?.windows[selected] ? selected : available[0];
  const g = data.growth_of_100k;
  return <div style={stack}>
    <div style={{ display: "flex", gap: 6 }}>{available.map((w) => <button key={w} style={button(key === w)} onClick={() => setSelected(w)}>{w === "all" ? "All history" : w.toUpperCase()}</button>)}</div>
    <CashFlowCard data={data} window={key ? data.performance.windows[key] : undefined} label={key === "all" ? "all history" : key?.toUpperCase() ?? "unavailable"} />
    <Card title="Current-holdings XIRR" subtitle="Open positions only, shown beside the complete recorded cash-flow result."><Reading label="Annualised, money-weighted" value={rate(data.performance?.xirr_holdings)} detail="This selects transactions for currently held ISINs; fully exited investments are absent. It cannot measure the investor's whole history or adviser skill." /><CashFlowDisclosure data={data} /></Card>
    {g && <details style={note}><summary>Current-weight simulation · historical price path, not the investor's realised return</summary>
      <Card title="Simulation · growth of ₹1,00,000" subtitle={`${g.dates[0]} to ${g.dates[g.dates.length - 1]}; current weights rebalanced daily.`}>
        <WindowCoverage coverage={data.risk_coverage?.[data.risk["1y"] ? "1y" : "available"]} />
        <VintageLinePlot labels={g.dates} series={[{ label: "Current-holdings price simulation", points: g.portfolio, color: "var(--chart-1)", weight: 2.2 }, ...(g.benchmark ? [{ label: `${data.benchmark} · total return`, points: g.benchmark, color: "var(--ink-300)", weight: 1.8 }] : [])]} height={300} formatTick={(v) => money(v)} formatLabel={(d) => d.slice(0, 7)} baseline={{ value: "100000", label: "₹1,00,000" }} />
        <div style={note}>This diagnostic uses covered holdings only. Stock prices exclude cash dividends and rebalancing costs; the index includes reinvested dividends. The recorded cash-flow comparison above measures a different, explicitly stated outcome.</div>
      </Card>
    </details>}
  </div>;
}

const LENSES: Record<string, { label: string; x: keyof AnalyticsPosition; y: keyof AnalyticsPosition; xLabel: string; yLabel: string }> = {
  valuation: { label: "Valuation", x: "pe", y: "pb", xLabel: "P/E", yLabel: "P/B" },
  growth: { label: "Growth", x: "profit_growth_1y", y: "sales_growth_1y", xLabel: "Profit growth", yLabel: "Sales growth" },
  profitability: { label: "Profitability", x: "roe", y: "net_margin", xLabel: "ROE", yLabel: "Net margin" },
  quality: { label: "Balance sheet", x: "debt_to_equity", y: "interest_cover", xLabel: "D/E", yLabel: "Interest cover" },
};
const fractions = new Set(["roe", "net_margin", "sales_growth_1y", "profit_growth_1y"]);

export function ValuationMapPanel({ data }: { data: AnalyticsResponse }) {
  const [key, setKey] = useState("valuation");
  const [bubble, setBubble] = useState(false);
  const lens = LENSES[key] ?? LENSES.valuation!;
  const derived = data.derived.lenses[key];
  const positions = ordered(data);
  const num = (p: AnalyticsPosition, k: keyof AnalyticsPosition): number | null => typeof p[k] === "number" ? p[k] as number : null;
  const fmt = (v: number | null | undefined, k: string) => fractions.has(k) ? pct(v, 1) : k === "debt_to_equity" ? times(v) : ratio(v);
  const bubbles: BubblePoint[] = positions.filter((p) => derived?.position_statuses[p.isin] != null).map((p) => ({ key: p.isin, label: p.symbol ?? p.isin, x: num(p, lens.x)!, y: num(p, lens.y)!, weight: p.weight ?? 0, tone: "var(--chart-1)" }));
  const external = derived?.reference_basis?.startsWith("external:") ?? false;
  const referenceName = external ? derived!.reference_basis!.slice("external:".length) : "";
  return <Card title={external ? `Company characteristics · against ${referenceName}` : "Company characteristics · within-portfolio comparison"}
    subtitle={external ? `Each holding's P/E and P/B against the index publisher's own figures (observed ${data.fundamentals.benchmark_as_of}). A market reference, not a fair-value estimate.` : "Neutral comparisons to money-weighted medians among holdings with both inputs; not market valuations."}
    action={<button style={button(bubble)} onClick={() => setBubble(!bubble)}>{bubble ? "Show table" : "Show weighted bubbles"}</button>}>
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 14 }}>{Object.entries(LENSES).map(([k, v]) => <button key={k} style={button(key === k)} onClick={() => setKey(k)}>{v.label}</button>)}</div>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 14 }}>{Object.entries(derived?.status_weight_pct ?? {}).map(([status, weight]) => <Badge key={status} tone="neutral">{status}: {pp(weight, 1)} · {derived?.status_counts[status] ?? 0} names</Badge>)}<Badge tone="neutral">Unrated: {pp(derived?.unrated_weight_pct, 1)}</Badge></div>
    <div style={note}>{external ? `${referenceName} published` : "Reference medians"}: {lens.xLabel} {fmt(derived?.x_average, lens.x)} · {lens.yLabel} {fmt(derived?.y_average, lens.y)}. Both inputs required · <Coverage value={derived?.plottable_pct} /></div>
    {bubble ? <ScatterPlot points={bubbles} xLabel={lens.xLabel} yLabel={lens.yLabel} xAvg={derived?.x_average ?? null} yAvg={derived?.y_average ?? null} xLowerIsBetter={false} yLowerIsBetter={false} neutral format={(v, axis) => fmt(v, axis === "x" ? lens.x : lens.y)} /> : <DataTable dense columns={columns([{ key: "asset", label: "Holding" }, { key: "weight", label: "Value weight", align: "right" }, { key: "x", label: lens.xLabel, align: "right" }, { key: "y", label: lens.yLabel, align: "right" }, { key: "status", label: "Within-portfolio comparison" }])}
      rows={positions.map((p) => ({ id: p.isin, asset: <AssetCell position={p} />, weight: pp(p.weight, 1), x: fmt(num(p, lens.x), lens.x), y: fmt(num(p, lens.y), lens.y), status: derived?.position_statuses[p.isin] ?? `Not rated: missing ${[num(p, lens.x) == null ? lens.xLabel : null, num(p, lens.y) == null ? lens.yLabel : null].filter(Boolean).join(" / ") || "positive valuation inputs"}` }))} />}
    <div style={note}>{external ? "The index is large-cap; most of this book is small-cap, which usually trades on different multiples. Above or below the index is a description, not a buy/sell signal." : "Above or below a portfolio median is a relative description, not a good/bad signal."} Bubble size represents portfolio value weight.</div>
  </Card>;
}
