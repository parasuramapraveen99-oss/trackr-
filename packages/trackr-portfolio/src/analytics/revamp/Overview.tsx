"use client";

/**
 * Overview (review §4, comments 3–6).
 *
 * Basic: grade gauge (letter, score, band range; no verdict word), the core insight (the
 * first written line), one chip per fired rule, and Price Trend.
 * Advanced: two columns, Insights | Explanation, every fired finding from every tab, with
 * an ⓘ that shows the situation first and then Calculation / Why flagged.
 */

import { useState } from "react";
import { parseDecimal } from "@trackr/core";
import type { AnalyticsResponse, BasicCandidate, BasicTone } from "@trackr/core";
import { BasicInsights, NarrativeState, Tokens, useBasicNarrative } from "../../widgets/BasicInsights";
import { HealthParts, HealthScale, rawComponents } from "../HealthBoard";
import { CandidateFigures, CoverageFooter, Grid, Info, Panel, Rs, RuleCalc, TONE, ToneLegend, faint, findingLabel, fmtShare, muted } from "./common";

/** The only chips the review allows (§4 SWOT chips), and which finding each comes from. */
function chipLabel(c: BasicCandidate): string | null {
  switch (c.slot) {
    case "OV.SINGLE_NAME": return "Concentrated";
    case "DV.SECTOR": return "Sector-heavy";
    case "OV.VS_INDEX": return c.direction === "behind" ? "Behind index" : "Ahead of index";
    case "RR.SWINGS": return c.direction === "more_volatile" ? "High swings" : null;
    case "OV.QUALITY_CORE": return "Quality core";
    case "FD.PE": return c.direction === "cheaper" ? "Cheaper than index" : null;
    case "RR.LIQUIDITY": return "Hard to exit";
    case "FD.LOSS_MAKERS": return "Loss-making holdings";
    default: return null;
  }
}

const ORDER: Record<BasicTone, number> = { needs_attention: 0, watch: 1, positive: 2, neutral: 3, insufficient_data: 4 };

export function allCandidates(data: AnalyticsResponse): BasicCandidate[] {
  return Object.values(data.basic?.tabs ?? {}).flatMap((t) => t?.candidates ?? [])
    .sort((a, b) => ORDER[a.tone] - ORDER[b.tone]);
}

function Gauge({ data }: { data: AnalyticsResponse }) {
  const [open, setOpen] = useState(false);
  const h = data.health;
  const bands = h.bands.length ? h.bands : [["A", 80], ["B", 65], ["C", 50], ["D", 35], ["E", 20], ["F", 0]] as [string, number][];
  const band = h.grade ? bands.find(([g]) => g === h.grade) : undefined;
  const upper = band ? (bands[bands.indexOf(band) - 1]?.[1] ?? 101) - 1 : null;
  const measured = h.components.filter((c) => c.score != null).length;
  const raw = rawComponents(data);
  const missingQuality = raw.find((component) => component.key === "quality")?.evidence.input_errors;
  const missingQualityNotes = Array.isArray(missingQuality) ? missingQuality.filter((item): item is string => typeof item === "string").map((item) => {
    const [, isin, field] = item.split(":");
    const name = data.positions.find((position) => position.isin === isin)?.symbol ?? isin;
    if (!name) return "Some company inputs are unavailable.";
    const label = field ? field.replaceAll("_", " ") : "company classification";
    return `${name}: ${label} unavailable`;
  }) : [];
  return <Panel label="Portfolio health" span={5} info={<Info title="Portfolio health" calc={<>
    Five parts, weighted: {h.components.map((c) => `${c.label} ${Math.round(c.weight * 100)}%`).join(", ")}. Rule {h.rule_version}.
    {" "}Provisional: the rubric is not yet calibrated.</>}>
    A score out of 100 from five parts of the portfolio. It describes the portfolio, not the investor.
  </Info>}>
    <div style={{ textAlign: "center" }}>
      {h.grade != null
        ? <>
          <div style={{ font: "600 44px var(--font-figure)", lineHeight: 1 }}>{h.grade}</div>
          <div style={muted}>{h.score}/100 · {h.grade} band: {band?.[1]}–{upper}</div>
        </>
        : <>
          <div style={{ font: "600 30px var(--font-figure)", lineHeight: 1.1, color: "var(--text-secondary)" }}>Withheld</div>
          <div style={muted}>{measured} of {h.components.length} parts measured
            {h.score_bounds && <> · possible range {h.score_bounds.lower.toFixed(0)}–{h.score_bounds.upper.toFixed(0)}</>}.
            {" "}Missing parts are never filled with an average.</div>
        </>}
    </div>
    <HealthScale data={data} />
    <div style={faint}>Bands {bands.map(([g, f]) => `${g} ≥${f}`).join(" · ")}</div>
    <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
      style={{ alignSelf: "flex-start", border: 0, background: "none", padding: 0, cursor: "pointer", ...faint, color: "var(--text-link)" }}>
      {open ? "Hide the five parts" : "Show the five parts →"}
    </button>
    {open && <HealthParts data={data} />}
    <details style={faint}><summary>Coverage of each part</summary>
      <p>A part needs inputs for at least 80% of included portfolio value. A grade stays provisional; missing parts are not filled.</p>
      {data.health.components.map((component) => <div key={component.key}>
        <b>{component.label}:</b> {component.evidence.coverage != null
          ? `${(component.evidence.coverage * 100).toFixed(1)}% of included value`
          : component.score == null ? "Not measured" : "Measured"}
      </div>)}
      {missingQualityNotes.length > 0 && <p>Company quality leaves out: {missingQualityNotes.join("; ")}.</p>}
    </details>
    {(h.excluded?.length ?? 0) > 0 && <p style={faint}>Health covers the priced, reconciled subset. Left out: {h.excluded!.map((item) => item.name ?? item.isin).join(", ")}.</p>}
  </Panel>;
}

function Chips({ data }: { data: AnalyticsResponse }) {
  const facts = data.basic?.facts ?? {};
  const chips = allCandidates(data).map((c) => [c, chipLabel(c)] as const).filter(([, l]) => l);
  if (!chips.length) return null;
  return <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
    {chips.map(([c, label]) => <span key={c.slot} style={{
      display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 9px", borderRadius: 999,
      border: `1px solid ${TONE[c.tone].color}`, color: TONE[c.tone].color, font: "600 11.5px var(--font-label)",
    }}>● {label}<Info title={label ?? ""} calc={<RuleCalc candidate={c} />}>
      <CandidateFigures candidate={c} facts={facts} />
      <div style={{ ...faint, marginTop: 4 }}>Rule {data.basic?.rule_version}</div>
    </Info></span>)}
  </div>;
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
    <BasicInsights tab="overview" feature className="rv-overview-insights" style={{ gridColumn: "span 7", minWidth: 0 }}>
      <Chips data={data} />
    </BasicInsights>
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
