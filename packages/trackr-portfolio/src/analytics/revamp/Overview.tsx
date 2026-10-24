"use client";

/**
 * Overview (review §4, comments 3–6).
 *
 * Basic: grade gauge (letter, score, band range; no verdict word), the core insight (the
 * first written line) and Price Trend. The per-rule tags live in Advanced only.
 * Advanced: the five health parts, then two columns, Insights | Explanation, every fired finding from every tab, with
 * an ⓘ that shows the situation first and then Calculation / Why flagged.
 */

import { parseDecimal } from "@trackr/core";
import type { AnalyticsResponse, BasicCandidate, BasicTone } from "@trackr/core";
import { BasicInsights, NarrativeState, Tokens, useBasicNarrative } from "../../widgets/BasicInsights";
import { HealthParts } from "../HealthBoard";
import { CandidateFigures, CoverageFooter, Grid, Info, Panel, Rs, RuleCalc, TONE, ToneLegend, faint, findingLabel, fmtShare, muted } from "./common";

const ORDER: Record<BasicTone, number> = { needs_attention: 0, watch: 1, positive: 2, neutral: 3, insufficient_data: 4 };

export function allCandidates(data: AnalyticsResponse): BasicCandidate[] {
  return Object.values(data.basic?.tabs ?? {}).flatMap((t) => t?.candidates ?? [])
    .sort((a, b) => ORDER[a.tone] - ORDER[b.tone]);
}

/** Three-quarter ring, as in the review mockup: the grade arc when scored, otherwise the
 * possible range shaded with no letter. Band colours: A–B positive, C watch, D–F negative. */
function Ring({ score, grade, bounds }: { score: number | null; grade: string | null; bounds?: { lower: number; upper: number } | null }) {
  const C = 2 * Math.PI * 70 * 0.75;
  const arc = (from: number, to: number, stroke: string, opacity = 1) => <circle cx={100} cy={100} r={70} fill="none"
    style={{ stroke, strokeOpacity: opacity }} strokeWidth={14} strokeLinecap={from === 0 ? "round" : "butt"}
    strokeDasharray={`0 ${C * from / 100} ${C * (to - from) / 100} 999`} transform="rotate(135 100 100)" />;
  const color = score == null ? "var(--text-secondary)" : score >= 65 ? "var(--positive)" : score >= 50 ? "var(--warning)" : "var(--negative)";
  return <svg viewBox="0 0 200 175" width={210} style={{ maxWidth: "100%", display: "block", margin: "0 auto" }} role="img"
    aria-label={score != null ? `Grade ${grade}, ${score} out of 100` : "Grade withheld"}>
    {arc(0, 100, "var(--border)")}
    {score != null ? <>
      {arc(0, score, color)}
      <text x={100} y={104} textAnchor="middle" style={{ fill: color, font: "600 50px var(--font-figure)" }}>{grade}</text>
      <text x={100} y={130} textAnchor="middle" style={{ fill: "var(--text-secondary)", font: "500 14px var(--font-label)" }}>{score}/100</text>
    </> : <>
      {bounds && arc(bounds.lower, bounds.upper, "var(--warning)", 0.45)}
      <text x={100} y={100} textAnchor="middle" style={{ fill: "var(--text-primary)", font: "500 40px var(--font-figure)" }}>—</text>
      {bounds && <text x={100} y={126} textAnchor="middle" style={{ fill: "var(--text-secondary)", font: "500 12px var(--font-label)" }}>
        range {bounds.lower.toFixed(0)}–{bounds.upper.toFixed(0)}</text>}
    </>}
  </svg>;
}

function Gauge({ data }: { data: AnalyticsResponse }) {
  const h = data.health;
  const bands = h.bands.length ? h.bands : [["A", 80], ["B", 65], ["C", 50], ["D", 35], ["E", 20], ["F", 0]] as [string, number][];
  const band = h.grade ? bands.find(([g]) => g === h.grade) : undefined;
  const upper = band ? (bands[bands.indexOf(band) - 1]?.[1] ?? 101) - 1 : null;
  const measured = h.components.filter((c) => c.score != null).length;
  return <Panel label="Portfolio health analysis" span={5} info={<Info title="Portfolio health" calc={<>
    Five parts, weighted: {h.components.map((c) => `${c.label} ${Math.round(c.weight * 100)}%`).join(", ")}. Rule {h.rule_version}.
    {" "}Provisional: the rubric is not yet calibrated.</>}>
    A score out of 100 from five parts of the portfolio. It describes the portfolio, not the investor.
  </Info>}>
    <div style={{ textAlign: "center" }}>
      <Ring score={h.grade != null ? h.score : null} grade={h.grade} bounds={h.score_bounds} />
      {h.grade != null
        ? <div style={muted}>{h.grade} band: {band?.[1]}–{upper}</div>
        : <>
          <div style={{ fontWeight: 600 }}>Grade withheld</div>
          <div style={muted}>{measured} of {h.components.length} parts measured. Missing parts are never filled with an average.</div>
        </>}
    </div>
    {(h.excluded?.length ?? 0) > 0 && <p style={faint}>Health covers the priced, reconciled subset. Left out: {h.excluded!.map((item) => item.name ?? item.isin).join(", ")}.</p>}
  </Panel>;
}

const STATES: [string, string, BasicTone, string][] = [
  ["Strong", "STRONG", "positive", "In profit, and the price is above its 50-day average."],
  ["Weakening", "WEAKENING", "watch", "In profit, but the price is below its 50-day average."],
  ["Caution", "CAUTION", "needs_attention", "At a loss, and the price is below its 50-day average."],
  ["Recovery", "RECOVERY", "neutral", "At a loss, but the price is back above its 50-day average."],
  ["Not classified", "UNKNOWN", "insufficient_data", "A price trend cannot be established from the available history."],
];

function PriceTrend({ data }: { data: AnalyticsResponse }) {
  const money = data.capital_state ?? {};
  const pct = data.derived.capital_state_pct ?? {};
  const total = data.derived.capital_state_total;
  const partial = data.coverage.basis === "priced_subset";
  const worth = partial ? data.coverage.known_value_subtotal : data.kpi.market_value;
  const reconciles = total != null && worth != null && !!parseDecimal(total)?.equals(parseDecimal(worth) ?? "NaN");
  if (worth == null || total == null) return <Panel label="Price Trend"><div style={muted}>Needs every holding priced on this date; not shown while some are unpriced.</div></Panel>;
  return <Panel label="Price Trend" info={<Info title="Price Trend">
    Each holding sits in one state by whether it is in profit and where its price is against its 50-day moving
    average. Percentages are shares of current portfolio value, not counts of stocks or distance from the average.
    {STATES.map(([l, , , d]) => <div key={l} style={{ marginTop: 4 }}><b>{l}:</b> {d}</div>)}
  </Info>}>
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
      {STATES.filter(([, key]) => key !== "UNKNOWN" || (parseDecimal(money.UNKNOWN)?.gt(0) ?? false)).map(([label, key, tone, desc]) => <div key={key} title={desc} style={{ padding: "10px 12px", borderRadius: 8, background: TONE[tone].tint }}>
        <div style={{ font: "600 12px var(--font-label)", color: TONE[tone].color }}>● {label}</div>
        <div style={{ font: "600 17px var(--font-figure)" }}><Rs v={money[key] ?? null} /></div>
        <div style={faint}>{fmtShare(pct[key])} of {partial ? "included" : "portfolio"} value</div>
      </div>)}
    </div>
    {Object.keys(money).length > 0 && <div style={{ ...faint, color: reconciles ? undefined : "var(--negative)" }}>
      {reconciles ? <>The states add up to the {partial ? "priced subtotal" : "portfolio's worth"} (<Rs v={worth} />).</>
        : <>The states do not add up to the portfolio&apos;s worth (<Rs v={total} /> vs <Rs v={worth} />). Treat these boxes as unchecked.</>}
    </div>}
    {partial && <p style={faint}>Unpriced or incomplete holdings are excluded: {(data.health.excluded ?? []).map((item) => item.name ?? item.isin).join(", ")}.</p>}
  </Panel>;
}

export function OverviewBasic({ data }: { data: AnalyticsResponse }) {
  return <div className="rv-grid rv-overview" style={{ display: "grid", gridTemplateColumns: "repeat(12, minmax(0, 1fr))", gap: 12 }}>
    <Gauge data={data} />
    <BasicInsights tab="overview" feature header={false} label="Portfolio highlights" className="rv-overview-insights" style={{ gridColumn: "span 7", minWidth: 0 }} />
    <PriceTrend data={data} />
    <CoverageFooter data={data} />
  </div>;
}

export function OverviewAdvanced({ data }: { data: AnalyticsResponse }) {
  const narrative = useBasicNarrative("insights");
  const lines = new Map((narrative.data?.lines ?? []).map((l) => [l.slot, l]));
  const state = NarrativeState({ narrative });
  const facts = data.basic?.facts ?? {};
  const rows = allCandidates(data);
  const missing = Object.values(data.basic?.tabs ?? {}).flatMap((t) => t?.insufficient ?? []);
  return <Grid>
    <Panel label="Portfolio health · the five parts" info={<Info title="The five parts">
      Each bar is one part&apos;s score out of 100, weighted as shown. A hatched bar is a part that could not be measured;
      it is left out, never filled with an average.
    </Info>}>
      <HealthParts data={data} />
    </Panel>
    <Panel label="Every fired insight" right={<ToneLegend />}>
      {state}
      <div className="rv-scroll"><table className="rv-table">
        <thead><tr><th style={{ width: "28%" }}>Insights</th><th>Explanation</th></tr></thead>
        <tbody>{rows.map((c) => {
          const line = lines.get(c.slot);
          return <tr key={c.slot}>
            <td><span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <b style={{ color: TONE[c.tone].color }}>{findingLabel(c)}</b>
              <Info title={findingLabel(c)} calc={<RuleCalc candidate={c} />}>
                <CandidateFigures candidate={c} facts={facts} />
              </Info>
            </span></td>
            <td>{line ? <><b><Tokens tokens={line.lead} /></b> <Tokens tokens={line.body} /></>
              : <span style={muted}><CandidateFigures candidate={c} facts={facts} /></span>}</td>
          </tr>;
        })}</tbody>
      </table></div>
      {missing.length > 0 && <div style={faint}>
        Not measured yet: {missing.map((m) => m.rule?.label ?? m.slot).join(", ")}. Each says why on its own tab.
      </div>}
      <div style={faint}>Rule version {data.basic?.rule_version}. Colours follow the published thresholds and cannot be changed by the written text.</div>
    </Panel>
    <CoverageFooter data={data} />
  </Grid>;
}
