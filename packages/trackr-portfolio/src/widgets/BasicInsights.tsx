"use client";

/**
 * The Analytics Basic view's one-liners for one tab (docs/ANALYTICS_BASIC_NARRATIVE.md).
 *
 * Every word of a line was written by the model and checked by the server (V1-V13); every
 * number is a fact token rendered here through format.ts and, for money, <Figure>, so the
 * privacy mask still works and nothing can show a figure the server did not send. The
 * dot colour is the TONE code set from the published thresholds; the model cannot move it.
 *
 * No placeholder sentence in any state. Each state says what is true: off on this
 * deployment, consent not given, not written yet, or a named failure.
 */

import { useState, type CSSProperties, type ReactNode } from "react";
import { Figure, api, useAuth, useFetch, CONSENT_NOTICE_VERSION } from "@trackr/core";
import type { BasicLine, BasicNarrativeResponse, BasicTab, BasicTone, NarrativeToken } from "@trackr/core";
import { Skeleton, cardTitle } from "@trackr/ui";
import { EMPTY, money, pct, pp, ratio } from "../analytics/format";
import { CARD_SHELL } from "../analytics/revamp/common";

const TONE: Record<BasicTone, { color: string; label: string }> = {
  needs_attention: { color: "var(--negative)", label: "Needs attention" },
  watch: { color: "var(--warning)", label: "Watch" },
  positive: { color: "var(--positive)", label: "Positive" },
  neutral: { color: "var(--text-tertiary)", label: "Neutral" },
  insufficient_data: { color: "var(--ink-300)", label: "Not measured yet" },
};

export const BASIC_TITLES: Record<BasicTab, [string, string]> = {
  overview: ["Your portfolio, in short", "The findings that matter most right now"],
  risk: ["Risk & return, in short", "How bumpy the ride is, and how bad it has got"],
  diversification: ["How your money is spread", "Your portfolio's sector exposure, company-size mix and how holdings move together"],
  fundamentals: ["Are you paying a fair price?", "Your portfolio against the Nifty 50, in plain terms"],
  performance: ["How it has done", "Your money against the same money in the index"],
  insights: ["Every finding", "Each fired rule, explained with this portfolio's figures"],
};

/** Ratios read as multiples ("2.08×") only where the figure IS a multiple. Beta, a slope,
 * stays a bare number. Keyed on the fact id's leaf, like the server's registry. */
const MULTIPLE_LEAVES = new Set(["vol_ratio_1y", "pe", "pb", "bench_pe", "bench_pb"]);

function Token({ token }: { token: NarrativeToken }) {
  if (token.t === "text") return <>{token.v}</>;
  const { value, unit, digits, id } = token;
  if (value == null) return <>{EMPTY}</>;
  switch (unit) {
    case "money":
      return <Figure>{money(typeof value === "string" ? value : null)}</Figure>;
    case "fraction":
      return <b>{pct(Number(value), digits ?? 1)}</b>;
    case "percent":
      return <b>{pp(Number(value), digits ?? 1)}</b>;
    case "ratio":
      return <b>{ratio(Number(value), digits ?? 2)}{MULTIPLE_LEAVES.has(id?.split(".").pop() ?? "") ? "×" : ""}</b>;
    case "count":
      return <b>{ratio(Number(value), 0)}</b>;
    default:
      return <b>{String(value)}</b>;
  }
}

export function Tokens({ tokens }: { tokens: NarrativeToken[] }) {
  return <>{tokens.map((token, i) => <Token key={i} token={token} />)}</>;
}

export function Dot({ tone, size = 8 }: { tone: BasicTone; size?: number }) {
  const t = TONE[tone];
  return <span aria-label={t.label} title={t.label} style={{
    flex: "0 0 auto", width: size, height: size, borderRadius: size, display: "inline-block",
    background: tone === "insufficient_data" ? "transparent" : t.color, border: `1.5px solid ${t.color}`,
  }} />;
}

function Line({ line, large = false }: { line: BasicLine; large?: boolean }) {
  return (
    <li style={{ display: "flex", gap: 10, alignItems: "baseline", padding: large ? "4px 0 10px" : "7px 0",
      fontSize: large ? 18 : 13.5, lineHeight: large ? 1.35 : 1.5, fontFamily: large ? "var(--font-display, Georgia, serif)" : undefined }}>
      <span style={{ transform: "translateY(-1px)" }}><Dot tone={line.tone} size={large ? 10 : 8} /></span>
      <span><b><Tokens tokens={line.lead} /></b> <Tokens tokens={line.body} /></span>
    </li>
  );
}

const muted = { font: "500 12.5px var(--font-label)", color: "var(--text-secondary)" } as const;
const action = {
  alignSelf: "flex-start", background: "none", border: "1px solid var(--border)", borderRadius: 6,
  padding: "4px 10px", font: "600 12px var(--font-label)", color: "var(--text-link)", cursor: "pointer",
} as const;

/** The read, the explicit "write" and the consent grant, for one tab. */
export function useBasicNarrative(tab: BasicTab) {
  const { investorId, token, asOf } = useAuth();
  const read = useFetch<BasicNarrativeResponse>(
    investorId ? `/investors/${investorId}/narrative/basic` : null,
    { tab, as_of: asOf },
    token,
  );
  const [written, setWritten] = useState<BasicNarrativeResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const data = written && written.tab === tab && written.as_of === read.data?.as_of ? written : read.data;

  async function write() {
    setBusy(true);
    setFailure(null);
    try {
      setWritten(await api.post<BasicNarrativeResponse>(
        `/investors/${investorId}/narrative/basic/generate`,
        { query: { tab, as_of: asOf }, token },
      ));
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "The summary could not be written.");
    } finally {
      setBusy(false);
    }
  }

  async function consent() {
    setBusy(true);
    setFailure(null);
    try {
      await api.post(`/investors/${investorId}/consent`, {
        json: { notice_version: CONSENT_NOTICE_VERSION, evidence_ref: "ui:analytics-basic-insights", purpose: "ANALYTICS_INSIGHTS" },
        token,
      });
      read.refetch();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "Consent could not be recorded.");
    } finally {
      setBusy(false);
    }
  }

  return { data, error: read.error, busy, failure, write, consent };
}

/** The non-ready states, shared by every place that shows written lines. */
export function NarrativeState({ narrative }: { narrative: ReturnType<typeof useBasicNarrative> }): ReactNode {
  const { data, error, busy, write, consent } = narrative;
  if (error && !data) return <div style={{ ...muted, color: "var(--negative)" }}>Summary unavailable: {error}</div>;
  if (!data) return <Skeleton height={96} />;
  if (data.state === "disabled") return <div style={muted}>Written summaries are turned off on this deployment. The figures are in Advanced.</div>;
  if (data.state === "consent_required") return <>
    <div style={muted}>
      Written insights let the assistant read this portfolio&apos;s holdings, transactions and
      analytics to write these lines. Your PAN, any password and the portfolio&apos;s name are never sent.
    </div>
    <button type="button" style={action} disabled={busy} onClick={consent}>{busy ? "Saving…" : "Turn on written insights"}</button>
  </>;
  if (data.state === "not_generated") return <>
    <div style={muted}>No summary has been written for these figures yet.</div>
    <button type="button" style={action} disabled={busy} onClick={write}>{busy ? "Writing…" : "Write summary"}</button>
  </>;
  if (!data.lines?.length) return <>
    <div style={muted}>No written line passed the checks for today&apos;s figures, so none is shown.</div>
    <button type="button" style={action} disabled={busy} onClick={write}>{busy ? "Writing…" : "Retry"}</button>
  </>;
  return null;
}

export function BasicInsights({ tab, feature = false, header = true, label, children, className, style }: {
  tab: BasicTab; feature?: boolean; header?: boolean; label?: string; children?: ReactNode; className?: string; style?: CSSProperties;
}) {
  const narrative = useBasicNarrative(tab);
  const [title] = BASIC_TITLES[tab];
  const state = NarrativeState({ narrative });
  const lines = narrative.data?.lines ?? [];

  return (
    <section aria-label={label ?? title} className={className} style={{ gridColumn: "1 / -1", display: "flex", flexDirection: "column", gap: 8, ...CARD_SHELL, ...style }}>
      {!header && label && <div style={cardTitle}>{label}</div>}
      {header && <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <div>
          <div style={cardTitle}>{title}</div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", font: "500 11px var(--font-label)", color: "var(--text-tertiary)" }}>
          {(["needs_attention", "watch", "positive", "neutral"] as BasicTone[]).map((key) => (
            <span key={key} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <span style={{ width: 7, height: 7, borderRadius: 4, background: TONE[key].color }} />{TONE[key].label}
            </span>
          ))}
        </div>
      </div>}
      {state ?? <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {lines.map((line, i) => <Line key={line.slot} line={line} large={feature && i === 0} />)}
      </ul>}
      {narrative.data?.state === "ready" && lines.length > 0 && !!narrative.data.rejected?.length && <>
        <div role="status" style={muted}>Some generated lines could not be checked and are not shown.</div>
        <button type="button" style={action} disabled={narrative.busy} onClick={narrative.write}>
          {narrative.busy ? "Writing…" : "Retry"}
        </button>
      </>}
      {children}
      {narrative.failure && <div role="alert" style={{ ...muted, color: "var(--negative)" }}>{narrative.failure}</div>}
    </section>
  );
}
